"""Isolated synthetic native PAPER case; no operational bot, wallet or provider."""
import asyncio
import datetime as dt
import json
import sys
import time
from pathlib import Path
expected = Path(sys.argv.pop(1)).resolve()
options = json.loads(sys.argv.pop(1))
assert Path(__file__).resolve().parents[1] == expected
sys.path.insert(0, str(expected))
if options.get('log'):
    sys.argv.append('--log')
case = options['case']
attempts = []
def audit(event, args):
    if event in {'socket.getaddrinfo', 'socket.connect'}:
        address = args[0] if event == 'socket.getaddrinfo' else args[1][0]
        if address not in {'127.0.0.1', '::1', 'localhost', None}:
            attempts.append(event)
            raise PermissionError('External network forbidden in isolated integration')
    if event == 'subprocess.Popen':
        attempts.append(event)
        raise PermissionError('Child process forbidden in isolated integration')
sys.addaudithook(audit)
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
calls = []
factor = 1.
born = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)
birth = {'version':'birdeye_mint_creation_receipt_v1','source':'birdeye','chain':'solana',
    'address':TOKEN,'created_at':born.isoformat(),'slot':123,'tx_hash':'1'*64,
    'received_at':time.time()-.01,'basis':'provider_reported_mint_creation_not_independent_chain_verification'}
async def snapshot(address, **kwargs):
    calls.append('snapshot')
    return stamp_market_observation({'address':address,'chain':'solana','symbol':'SYNTHETIC',
        'dex_id':'pumpswap','price_usd':.01,'liquidity_usd':50000.,'market_cap_usd':100000.,
        'fdv_usd':150000.,'volume_24h_usd':10000000.,'holders':10000,
        'txns_last_5m':1000,'txns_last_5m_buys':900,'txns_last_5m_sells':100,
        'price_pct_5m':options.get('momentum',5000.),'price_pct_1m':500.,'volume_pct_5m':1000.,
        'liquidity_usd_is_proxy':False,'liquidity_is_proxy':False,
        'price_source':'dexscreener','price_confidence':'high'}, 'dexscreener')
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
    return stamp_market_observation({'address':address,'price_usd':.01 * factor,
        'liquidity_usd':50000.,'price_source':'dexscreener'},'dexscreener')
price_service.get_price = market_price
rugcheck.fetch_observation = rug
helius_cluster.fetch_observation = cluster
jupiter_router.get_routing_quote = quote
jupiter_price._fetch_batch_with_status = price_batch
sol_price.get_sol_usd_observation = fx
async def exercise():
    global factor
    await run_bot.async_init_db()
    run_bot._wallet_sol_balance = 10.
    report = {'progression':[]}
    try:
        async with run_bot.SessionLocal() as ses:
            await run_bot._evaluate_and_buy_guarded({'address':TOKEN,'chain':'solana','created_at':born,
                FIELD:birth,'discovered_via':'dex','dex_id':'pumpswap'},ses,source='dex')
            positions = (await ses.execute(select(Position))).scalars().all()
            if case == 'positive':
                assert len(positions)==1 and positions[0].buy_amount_sol==.1 and positions[0].dry_run is True
                p = positions[0]
                from trader import papertrading
                primary = papertrading._PORTFOLIO[TOKEN]
                report['entry_qty'] = p.entry_qty
                report['entry_signal'] = p.buy_price_pct_5m
                report['entry_run_id'] = p.run_id
                report['entry_identity_matches'] = p.source_position_key=='buy:'+primary['entry_intent_id']
                assert not run_bot._BUY_RECOVERY.pending_addresses
                # A duplicate actual dispatch cannot submit or persist another fill.
                await run_bot._evaluate_and_buy_guarded({'address':TOKEN,'chain':'solana',
                    'created_at':born,FIELD:birth,'discovered_via':'dex','dex_id':'pumpswap'},
                    ses,source='dex')
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
                    fills = (await ses.execute(select(TradeEvent).where(TradeEvent.position_id==p.id))).scalars().all()
                    report['sql_events'] = [{'event':event.event_type,'qty':event.qty} for event in fills]
                    assert sum(event.qty for event in fills)==p.entry_qty
            report['positions'] = [{'address':p.address,'closed':p.closed,'amount_sol':p.buy_amount_sol,
                'qty':p.qty,'lane':p.entry_lane,'dry_run':p.dry_run,'run_id':p.run_id} for p in positions]
            report['pending_buys'] = len(run_bot._BUY_RECOVERY.pending_addresses)
            report['pending_sells'] = len(run_bot._SELL_RECOVERY.pending_addresses)
    finally:
        await jupiter_price.aclose()
        await engine.dispose()
    return report
report = asyncio.run(exercise())
assert not attempts, attempts
events_path = expected/'data/metrics/runtime_events.jsonl'
events = [json.loads(line) for line in events_path.read_text(encoding='utf-8-sig').splitlines()] if events_path.exists() else []
print(json.dumps({**report,'native_complete_entry':True,'boundary_calls':calls,
    'events':[{'event':e.get('event_type') or e.get('event'),'reason':e.get('reason'),
        'stage':e.get('stage'),'hard_failures':e.get('hard_failures')} for e in events],
    'stats':run_bot._stats,'external_attempts':attempts,
    'isolated_native_import':True,'scoped_modules':len(scoped),
    'operator_files_copied':0,'outbound_network_attempts':len(attempts),
    'dry_run':CFG.DRY_RUN,'exact_paper_size':CFG.PAPER_EXACT_TRADE_SIZE_SOL,
    'scope':'Native source-only PAPER lifecycle; no operational main loop, wallet or providers.'}))
