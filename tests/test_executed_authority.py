"""Original signed dummy SDK packets + synthetic RPC; no real wallet/provider."""
import base64
import copy
from datetime import datetime,timezone
import inspect

import base58
import pytest
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta,Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders import system_program

from execution import authority_observations as authority,chain_reconciliation as chain
from execution import jupiter_managed_contract as contract,unsigned_projection
from chain_fixtures import INPUT_ACCOUNT,OUTPUT_ACCOUNT,POOL,evidence,simulation
from test_live_signing_boundaries import signer
from test_jupiter_managed_contract import order_payload,execution_payload,TOKEN,AMOUNT

SYSTEM=system_program.ID
LEGACY=Pubkey.from_string('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
TOKEN22=Pubkey.from_string('TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb')
SOURCE='a'*64


def prepared(signer,*,version=2,loaded=False,side='buy',confirmation='finalized'):
    wallet=signer.PUBLIC_KEY
    accounts=[AccountMeta(wallet,True,True),AccountMeta(INPUT_ACCOUNT,False,True),
        AccountMeta(OUTPUT_ACCOUNT,False,True),AccountMeta(POOL,False,True),
        *[AccountMeta(key,False,False) for key in (SYSTEM,LEGACY,TOKEN22,Pubkey.from_string(chain.SOL))]]
    route=Instruction(Pubkey.new_unique(),b'owned synthetic route',accounts)
    tables=[AddressLookupTableAccount(Pubkey.new_unique(),[SYSTEM,LEGACY,TOKEN22])] if loaded else []
    message=MessageV0.try_compile(wallet,[route],tables,Hash.new_unique())
    raw=order_payload(str(wallet),amount=AMOUNT if side=='buy' else 400)
    if side=='sell':raw.update(inputMint=TOKEN,outputMint=chain.SOL,feeMint=TOKEN)
    raw['transaction']=base64.b64encode(bytes(VersionedTransaction.populate(message,[Signature.default()]))).decode()
    request=contract.ManagedRequest(raw['inputMint'],raw['outputMint'],int(raw['inAmount']),str(wallet),100)
    order=contract.check_order(raw,request)
    def complete(meta):
        # Fixture generator supplies static-vector positions; add original
        # read-only ALT slots without altering any native/token effects.
        addresses=[str(tables[0].addresses[i]) for i in message.address_table_lookups[0].readonly_indexes] if loaded else []
        meta['loadedAddresses']={'writable':[],'readonly':addresses}
        meta['preBalances'].extend([1000]*len(addresses));meta['postBalances'].extend([1000]*len(addresses))
        return meta
    bank=simulation(order);complete(bank['value'])
    projection=unsigned_projection.check(order,bank,rpc_source_sha256=SOURCE,
        observed_at=datetime.now(timezone.utc).isoformat(),current_node_slot=449,max_wallet_fee_lamports=6000)
    signed=signer.sign_base64_transaction(raw['transaction'])
    capsule=chain.make_capsule(order,signed,contract.check_signed_packet(order,signed),
        rpc_source_sha256=SOURCE,max_wallet_fee_lamports=6000,
        **({'unsigned_projection':projection} if version==2 else {}))
    execute=execution_payload(raw,signer)
    tx,status=evidence(capsule,execute,confirmation=confirmation);complete(tx['meta'])
    tx['meta']['innerInstructions']=[]
    return order,capsule,execute,tx,status


def compiled(order,meta,program,accounts,data):
    keys=[str(k) for k in order.transaction.message.account_keys]
    keys+=meta['loadedAddresses']['writable']+meta['loadedAddresses']['readonly']
    return {'programIdIndex':keys.index(str(program)),'accounts':[keys.index(str(k)) for k in accounts],
        'data':base58.b58encode(bytes(data)).decode(),'stackHeight':2}


def install(meta,ix):meta['innerInstructions']=[{'index':0,'instructions':[ix]}]


@pytest.mark.parametrize('version',[1,2])
@pytest.mark.parametrize('loaded',[False,True])
@pytest.mark.parametrize('kind',['assign','allocate'])
def test_actual_system_mutation_cannot_be_a_financial_receipt_despite_clean_simulation(signer,version,loaded,kind):
    order,cap,ex,tx,status=prepared(signer,version=version,loaded=loaded)
    ix=(system_program.assign({'pubkey':signer.PUBLIC_KEY,'owner':LEGACY}) if kind=='assign'
        else system_program.allocate({'pubkey':signer.PUBLIC_KEY,'space':16}))
    install(tx['meta'],compiled(order,tx['meta'],SYSTEM,[a.pubkey for a in ix.accounts],ix.data))
    if version==2:assert cap['unsigned_projection']['simulation']['value']['innerInstructions']==[]
    with pytest.raises(ValueError,match='wallet system-account mutation'):chain.reconcile(cap,ex,tx,status)


@pytest.mark.parametrize('loaded',[False,True])
def test_documented_compiled_transaction_cpi_has_original_account_resolution(signer,loaded):
    order,_,_,tx,_=prepared(signer,loaded=loaded)
    ix=system_program.transfer({'from_pubkey':signer.PUBLIC_KEY,'to_pubkey':POOL,'lamports':AMOUNT})
    install(tx['meta'],compiled(order,tx['meta'],SYSTEM,[a.pubkey for a in ix.accounts],ix.data))
    kwargs={'recording':'transaction'} if 'recording' in inspect.signature(authority.check).parameters else {}
    # The old reader must fail on the actual indexed format, not a new kwarg.
    authority.check(order,tx['meta'],**kwargs)


@pytest.mark.parametrize('version',[1,2])
@pytest.mark.parametrize('loaded',[False,True])
@pytest.mark.parametrize('side',['buy','sell'])
@pytest.mark.parametrize('confirmation',['confirmed','finalized'])
def test_original_compiled_execution_is_checked_without_changing_finality(signer,version,loaded,side,confirmation):
    order,cap,ex,tx,status=prepared(signer,version=version,loaded=loaded,side=side,confirmation=confirmation)
    ix=system_program.transfer({'from_pubkey':signer.PUBLIC_KEY if side=='buy' else POOL,
        'to_pubkey':POOL if side=='buy' else signer.PUBLIC_KEY,
        'lamports':AMOUNT if side=='buy' else int(ex['totalOutputAmount'])})
    install(tx['meta'],compiled(order,tx['meta'],SYSTEM,[a.pubkey for a in ix.accounts],ix.data))
    receipt=chain.reconcile(cap,ex,tx,status)
    assert receipt['executed_wallet_authority_observations_checked'] is True
    assert receipt['financial_finality_verified'] is (confirmation=='finalized')
    assert receipt['requires_reconciliation'] is (confirmation!='finalized')
    assert not receipt['unsigned_swap_instructions_verified'] and not receipt['rpc_block_inclusion_cryptographically_verified']
    assert chain.validate_receipt(cap,ex,receipt)==receipt


@pytest.mark.parametrize('program',[LEGACY,TOKEN22])
@pytest.mark.parametrize('opcode',[4,6,13])
@pytest.mark.parametrize('loaded',[False,True])
@pytest.mark.parametrize('version',[1,2])
def test_executed_spl_delegation_or_close_authority_is_not_hidden_by_balances(signer,program,opcode,loaded,version):
    order,cap,ex,tx,status=prepared(signer,loaded=loaded,version=version)
    accounts=[INPUT_ACCOUNT,POOL,signer.PUBLIC_KEY]
    data=bytes([opcode])+(1000).to_bytes(8,'little')
    if opcode==6:accounts=[INPUT_ACCOUNT,signer.PUBLIC_KEY];data=bytes([6,3,1])+bytes(POOL)
    if opcode==13:accounts.insert(1,Pubkey.from_string(chain.SOL));data+=bytes([9])
    install(tx['meta'],compiled(order,tx['meta'],program,accounts,data))
    with pytest.raises(ValueError,match='delegation or authority'):chain.reconcile(cap,ex,tx,status)


FAULTS=('missing','null','not_list','duplicate_group','bad_group','bool_group_index','foreign_group_index',
    'missing_instructions','null_instruction','missing_program','bool_program','negative_program','foreign_program',
    'float_program','text_program','missing_accounts','text_accounts','bool_account','negative_account',
    'foreign_account','float_account','text_account','missing_data','bool_data','bad_base58','oversized_data',
    'ambiguous_program','ambiguous_parsed','parsed_only','partially_decoded_only')


def corrupt(order,meta,fault):
    ix=compiled(order,meta,SYSTEM,[order.request.taker,POOL],system_program.transfer({
        'from_pubkey':Pubkey.from_string(order.request.taker),'to_pubkey':POOL,'lamports':1}).data)
    install(meta,ix)
    keys=[str(k) for k in order.transaction.message.account_keys]+meta['loadedAddresses']['readonly']
    if fault=='missing':del meta['innerInstructions']
    elif fault=='null':meta['innerInstructions']=None
    elif fault=='not_list':meta['innerInstructions']={}
    elif fault=='duplicate_group':meta['innerInstructions']*=2
    elif fault=='bad_group':meta['innerInstructions']=[False]
    elif fault=='bool_group_index':meta['innerInstructions'][0]['index']=True
    elif fault=='foreign_group_index':meta['innerInstructions'][0]['index']=1
    elif fault=='missing_instructions':del meta['innerInstructions'][0]['instructions']
    elif fault=='null_instruction':meta['innerInstructions'][0]['instructions']=[None]
    elif fault=='missing_program':del ix['programIdIndex']
    elif fault.endswith('_program') and fault not in {'ambiguous_program'}:
        ix['programIdIndex']={'bool_program':True,'negative_program':-1,'foreign_program':len(keys),
            'float_program':1.,'text_program':'0'}[fault]
    elif fault=='missing_accounts':del ix['accounts']
    elif fault=='text_accounts':ix['accounts']='0'
    elif fault.endswith('_account'):
        ix['accounts'][0]={'bool_account':True,'negative_account':-1,'foreign_account':len(keys),
            'float_account':0.,'text_account':'0'}[fault]
    elif fault=='missing_data':del ix['data']
    elif fault=='bool_data':ix['data']=True
    elif fault=='bad_base58':ix['data']='not-base58-!'
    elif fault=='oversized_data':ix['data']='1'*1645
    elif fault=='ambiguous_program':ix['programId']=str(SYSTEM)
    elif fault=='ambiguous_parsed':ix['parsed']={'type':'transfer','info':{}}
    elif fault=='parsed_only':ix.clear();ix.update(programId=str(SYSTEM),parsed={'type':'transfer','info':{}})
    elif fault=='partially_decoded_only':
        ix.pop('programIdIndex');ix.update(programId=str(SYSTEM),accounts=[order.request.taker,str(POOL)])
    else:raise AssertionError(fault)


@pytest.mark.parametrize('fault',FAULTS)
@pytest.mark.parametrize('version',[1,2])
def test_missing_ambiguous_or_wrong_source_actual_recording_is_not_a_fill(signer,fault,version):
    order,cap,ex,tx,status=prepared(signer,version=version)
    corrupt(order,tx['meta'],fault)
    with pytest.raises(ValueError):chain.reconcile(cap,ex,tx,status)


@pytest.mark.parametrize('value',[None,True,0,'','parsed','compiled',[],{}])
def test_recording_source_is_explicit_and_closed(signer,value):
    order,_,_,tx,_=prepared(signer)
    with pytest.raises(ValueError):authority.check(order,tx['meta'],recording=value)


def test_indexed_actual_recording_cannot_masquerade_as_a_simulation(signer):
    order,_,_,tx,_=prepared(signer)
    ix=system_program.transfer({'from_pubkey':signer.PUBLIC_KEY,'to_pubkey':POOL,'lamports':1})
    install(tx['meta'],compiled(order,tx['meta'],SYSTEM,[a.pubkey for a in ix.accounts],ix.data))
    with pytest.raises(ValueError,match='JSON Parsed'):authority.check(order,tx['meta'])


@pytest.mark.parametrize('version',[1,2])
@pytest.mark.parametrize('flag',['missing','false','mutated_evidence'])
def test_old_flags_or_rechecksummed_receipts_cannot_replace_actual_authority_evidence(signer,monkeypatch,version,flag):
    order,cap,ex,tx,status=prepared(signer,version=version)
    if flag=='mutated_evidence':
        ix=system_program.assign({'pubkey':signer.PUBLIC_KEY,'owner':LEGACY})
        install(tx['meta'],compiled(order,tx['meta'],SYSTEM,[a.pubkey for a in ix.accounts],ix.data))
        with monkeypatch.context() as context:
            context.setattr(authority,'check',lambda *args,**kwargs:None)
            receipt=chain.reconcile(cap,ex,tx,status)
    else:
        receipt=chain.reconcile(cap,ex,tx,status)
        if flag=='missing':del receipt['executed_wallet_authority_observations_checked']
        else:receipt['executed_wallet_authority_observations_checked']=False
    receipt['rpc_evidence_sha256']=chain.digest(receipt['rpc_evidence'])
    original=copy.deepcopy(receipt)
    with pytest.raises(ValueError):chain.validate_receipt(cap,ex,receipt)
    assert receipt==original


@pytest.mark.parametrize('count,valid',[(1000,True),(1001,False)])
def test_existing_recording_bound_remains_enforced_for_actual_cpis(signer,count,valid):
    order,cap,ex,tx,status=prepared(signer)
    program=order.transaction.message.account_keys[order.transaction.message.instructions[0].program_id_index]
    ix=compiled(order,tx['meta'],program,[],b'fixture')
    tx['meta']['innerInstructions']=[{'index':0,'instructions':[copy.deepcopy(ix) for _ in range(count)]}]
    if valid:assert chain.reconcile(cap,ex,tx,status)['executed_wallet_authority_observations_checked']
    else:
        with pytest.raises(ValueError,match='bound'):chain.reconcile(cap,ex,tx,status)


@pytest.mark.asyncio
@pytest.mark.parametrize('side',['buy','sell'])
@pytest.mark.parametrize('fault',['system_mutation','token_authority','missing_recording','malformed_index'])
async def test_one_post_bad_actual_recording_stays_pending_without_rebuy_resell_or_fallback(
    managed,http,monkeypatch,tmp_path,side,fault):
    from unittest.mock import AsyncMock,Mock
    from fetcher import jupiter_router as router
    from runtime import owned_dispatch
    from runtime.buy_recovery import BuyRecoveryStore,BuyOutcomeUncertain
    from runtime.sell_recovery import SellRecoveryStore,SellOutcomeUncertain
    from trader import buyer,seller
    from utils import solana_execution
    from db.models import Position
    from test_jupiter_managed_contract import Response,buyer_guards
    order,_,execute,_,_=prepared(managed,side=side)
    http.replies.extend([Response(order.raw),Response(execute)])
    observed=[]
    async def read(cap,ex,*,endpoint):
        tx,status=evidence(cap,ex);meta=tx['meta']
        if fault=='system_mutation':
            ix=system_program.assign({'pubkey':managed.PUBLIC_KEY,'owner':LEGACY})
            install(meta,compiled(order,meta,SYSTEM,[a.pubkey for a in ix.accounts],ix.data))
        elif fault=='token_authority':
            install(meta,compiled(order,meta,LEGACY,[INPUT_ACCOUNT,POOL,managed.PUBLIC_KEY],bytes([4])+(1).to_bytes(8,'little')))
        elif fault=='missing_recording':meta['innerInstructions']=None
        else:corrupt(order,meta,'bool_program')
        observed.append(copy.deepcopy(meta['innerInstructions']))
        return chain.reconcile(cap,ex,tx,status)
    monkeypatch.setattr(solana_execution,'reconcile_original',read)
    sign=Mock(wraps=managed.sign_base64_transaction);monkeypatch.setattr(managed,'sign_base64_transaction',sign)
    if side=='buy':
        fallback=buyer_guards(monkeypatch)
        store=BuyRecoveryStore(tmp_path/'buy')
        with store.scope():
            attempt=store.begin(Position(address=TOKEN,token_mint=TOKEN,dry_run=False,buy_amount_sol=.1),paper=False,amount_sol=.1)
            with attempt.execution_scope(),pytest.raises(BuyOutcomeUncertain) as failure:await buyer.buy(TOKEN,.1)
        row=BuyRecoveryStore(store.directory)._records[attempt.intent_id]
    else:
        monkeypatch.setattr(seller,'jupiter',router);monkeypatch.setattr(seller,'_JUP_ROUTER_AVAILABLE',True)
        monkeypatch.setattr(seller,'JUP_SELL_SLIPPAGE_BPS',100)
        fallback=AsyncMock(side_effect=AssertionError('No fallback after actual POST'))
        monkeypatch.setattr(seller.gmgn,'sell',fallback)
        store=SellRecoveryStore(tmp_path/'sell')
        pos=Position(id=1,address=TOKEN,token_mint=TOKEN,dry_run=False,qty=1000,entry_qty=1000,
            buy_price_usd=1.,entry_notional_usd=10.,buy_amount_sol=.1,closed=False)
        attempt=store.begin(pos,400,paper=False,reason='synthetic')
        with attempt.execution_scope(),pytest.raises(SellOutcomeUncertain) as failure:await seller.sell(TOKEN,400)
        store.finish(attempt)
        row=SellRecoveryStore(store.directory).records[attempt.intent_id]
        assert pos.qty==1000 and not pos.closed
    assert len(observed)==1 and sign.call_count==1
    root=failure.value
    while root.__cause__ is not None:root=root.__cause__
    expected={'system_mutation':'wallet system-account mutation','token_authority':'delegation or authority',
        'missing_recording':'instruction recording is unavailable','malformed_index':'Invalid compiled executed instruction'}
    assert isinstance(root,ValueError) and expected[fault] in str(root)
    assert row['state']=='prepared' and 'fill' not in row and 'chain_receipt' not in row['execution']
    assert row['execution']['provider_response']['signature']==execute['signature']
    assert store.pending_addresses=={TOKEN} and owned_dispatch.pending_dispatch_count()==0
    fallback.assert_not_awaited()
    assert [call[0] for call in http.calls]==['GET','POST']


from test_jupiter_managed_contract import managed,http
