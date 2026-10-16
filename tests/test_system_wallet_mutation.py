"""Unsigned synthetic SDK packets/node-bank observations, never operator keys."""
import base64
from datetime import datetime,timezone

import base58
import pytest
from solders import system_program as system
from solders.hash import Hash
from solders.instruction import AccountMeta,Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from execution import authority_observations,jupiter_managed_contract as contract,unsigned_projection,wallet_effects
from chain_fixtures import INPUT_ACCOUNT,OUTPUT_ACCOUNT,POOL,simulation
from test_jupiter_managed_contract import order_payload,TOKEN,AMOUNT

WALLET=Keypair.from_seed(bytes([41])*32).pubkey()  # Public fixture seed only.
OWNER=Pubkey.from_string('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
SYSTEM=str(system.ID)


def system_instruction(kind,target,*,wallet=WALLET,authority=None):
    authority=wallet if authority is None else authority
    if kind=='assign':
        return system.assign({'pubkey':target,'owner':OWNER}),{'account':str(target),'owner':str(OWNER)}
    if kind=='allocate':
        return system.allocate({'pubkey':target,'space':16}),{'account':str(target),'space':16}
    if kind=='assignWithSeed':
        return system.assign_with_seed({'address':target,'base':wallet,'seed':'fixture','owner':OWNER}),{
            'account':str(target),'base':str(wallet),'seed':'fixture','owner':str(OWNER)}
    if kind=='allocateWithSeed':
        return system.allocate_with_seed({'address':target,'base':wallet,'seed':'fixture','space':16,'owner':OWNER}),{
            'account':str(target),'base':str(wallet),'seed':'fixture','space':16,'owner':str(OWNER)}
    if kind in {'createAccount','createAccountWithSeed'}:
        params={'from_pubkey':wallet,'to_pubkey':target,'lamports':1,'space':16,'owner':OWNER}
        info={'source':str(wallet),'newAccount':str(target),'lamports':1,'space':16,'owner':str(OWNER)}
        if kind=='createAccount':return system.create_account(params),info
        params.update(base=wallet,seed='fixture');info.update(base=str(wallet),seed='fixture')
        return system.create_account_with_seed(params),info
    if kind=='advanceNonce':
        return system.advance_nonce_account({'nonce_pubkey':target,'authorized_pubkey':authority}),{
            'nonceAccount':str(target),'nonceAuthority':str(authority)}
    if kind=='withdrawFromNonce':
        return system.withdraw_nonce_account({'nonce_pubkey':target,'authorized_pubkey':authority,
            'to_pubkey':POOL,'lamports':1}),{'nonceAccount':str(target),'nonceAuthority':str(authority),
            'destination':str(POOL),'lamports':1}
    if kind=='initializeNonce':
        return system.initialize_nonce_account({'nonce_pubkey':target,'authority':OWNER}),{
            'nonceAccount':str(target),'nonceAuthority':str(OWNER)}
    if kind=='authorizeNonce':
        return system.authorize_nonce_account({'nonce_pubkey':target,'authorized_pubkey':authority,'new_authority':OWNER}),{
            'nonceAccount':str(target),'nonceAuthority':str(authority),'newAuthorized':str(OWNER)}
    if kind=='upgradeNonce':
        return Instruction(system.ID,(12).to_bytes(4,'little'),[AccountMeta(target,False,True)]),{
            'nonceAccount':str(target)}
    if kind=='createAccountAllowPrefund':
        # Current official SystemInstruction variant13, absent from this SDK's
        # convenience constructors. Its NEW account is first, payer second.
        data=(13).to_bytes(4,'little')+(1).to_bytes(8,'little')+(16).to_bytes(8,'little')+bytes(OWNER)
        return Instruction(system.ID,data,[AccountMeta(target,True,True),AccountMeta(wallet,True,True)]),{
            'newAccount':str(target),'source':str(wallet),'lamports':1,'space':16,'owner':str(OWNER)}
    raise AssertionError(kind)


def fixture_order(kind,placement,*,wallet=WALLET,sponsor=None,target=None,authority=None):
    target=wallet if target is None else target
    system_ix,info=system_instruction(kind,target,wallet=wallet,authority=authority)
    accounts=[AccountMeta(wallet,True,True),AccountMeta(INPUT_ACCOUNT,False,True),
        AccountMeta(OUTPUT_ACCOUNT,False,True),AccountMeta(POOL,False,True),
        AccountMeta(system.ID,False,False)]
    for account in system_ix.accounts:
        if account.pubkey not in {item.pubkey for item in accounts}:
            accounts.append(AccountMeta(account.pubkey,False,account.is_writable))
    route=Instruction(Pubkey.new_unique(),b'owned synthetic swap fixture',accounts)
    instructions=[route]+([system_ix] if placement=='top' else [])
    payer=wallet if sponsor is None else sponsor.pubkey()
    message=MessageV0.try_compile(payer,instructions,[],Hash.new_unique())
    raw=order_payload(str(wallet),sponsor=sponsor)
    packet=VersionedTransaction.populate(message,[Signature.default()]*message.header.num_required_signatures)
    raw['transaction']=base64.b64encode(bytes(packet)).decode()
    request=contract.ManagedRequest(raw['inputMint'],raw['outputMint'],int(raw['inAmount']),str(wallet),100)
    order=contract.check_order(raw,request)
    bank=simulation(order)
    if placement!='top':
        if placement=='binary':
            inner={'programId':SYSTEM,'accounts':[str(a.pubkey) for a in system_ix.accounts],
                'data':base58.b58encode(bytes(system_ix.data)).decode()}
        else:
            assert placement=='parsed'
            inner={'programId':SYSTEM,'parsed':{'type':kind,'info':info}}
        bank['value']['innerInstructions']=[{'index':0,'instructions':[inner]}]
    return order,bank


def project(order,bank):
    return unsigned_projection.check(order,bank,rpc_source_sha256='a'*64,
        observed_at=datetime.now(timezone.utc).isoformat(),current_node_slot=449,max_wallet_fee_lamports=6000)


@pytest.mark.parametrize('kind',['assign','allocate'])
@pytest.mark.parametrize('placement',['top','binary','parsed'])
@pytest.mark.parametrize('sponsored',[False,True])
def test_wallet_owner_or_data_mutation_is_rejected_even_with_valid_balances(kind,placement,sponsored):
    sponsor=Keypair.from_seed(bytes([7])*32) if sponsored else None
    order,bank=fixture_order(kind,placement,sponsor=sponsor)
    effects=wallet_effects.inspect(order,bank['value'],max_wallet_fee_lamports=6000)
    assert effects['actual_input_units']==AMOUNT and effects['actual_output_units']==2_000_000
    assert not any(order.transaction.verify_with_results())
    with pytest.raises(ValueError,match='wallet system-account mutation'):
        project(order,bank)


KINDS=('createAccount','assign','createAccountWithSeed','advanceNonce','withdrawFromNonce',
    'initializeNonce','authorizeNonce','allocate','allocateWithSeed','assignWithSeed',
    'upgradeNonce','createAccountAllowPrefund')


@pytest.mark.parametrize('kind',[kind for kind in KINDS if kind not in {'assign','allocate'}])
@pytest.mark.parametrize('placement',['top','binary','parsed'])
def test_other_known_system_state_mutations_of_the_taker_are_not_authorized(kind,placement):
    # Seed/prefund/nonce packets exercise structural guard coverage, not a
    # claim that every fixture can execute against a real bank or wallet.
    order,bank=fixture_order(kind,placement)
    with pytest.raises(ValueError,match='wallet system-account mutation'):project(order,bank)


@pytest.mark.parametrize('kind',KINDS)
@pytest.mark.parametrize('placement',['top','binary','parsed'])
def test_wallet_payer_or_base_is_not_mistaken_for_the_mutated_destination(kind,placement):
    order,bank=fixture_order(kind,placement,target=POOL,authority=POOL)
    receipt=project(order,bank)
    assert receipt['unsigned_wallet_projection_checked'] and not receipt['unsigned_swap_instructions_verified']
    assert not receipt['fill_verified'] and not receipt['chain_wallet_verified']


@pytest.mark.parametrize('placement',['top','binary','parsed'])
def test_wallet_nonce_authority_cannot_be_reassigned_even_for_another_nonce_account(placement):
    order,bank=fixture_order('authorizeNonce',placement,target=POOL)
    with pytest.raises(ValueError,match='wallet system-account mutation'):project(order,bank)


@pytest.mark.parametrize('data',[b'',b'\x01',b'\x01\x00',b'\x01\x00\x00'])
def test_truncated_system_discriminator_never_authorizes_projection(data):
    order,bank=fixture_order('assign','binary')
    bank['value']['innerInstructions'][0]['instructions'][0]['data']=base58.b58encode(data).decode()
    with pytest.raises(ValueError):project(order,bank)


@pytest.mark.parametrize('parsed',[None,'assign',[],{}, {'type':'assign'},
    {'type':'assign','info':None},{'type':'assign','info':{}},
    {'type':'assign','info':{'account':True}},
    {'type':'assign','info':{'account':str(Pubkey.from_bytes(bytes([53])*32))}},
    {'type':'authorizeNonce','info':{'nonceAccount':str(POOL)}}])
def test_missing_or_unresolved_system_mutation_identity_stays_unknown(parsed):
    order,bank=fixture_order('assign','parsed')
    bank['value']['innerInstructions'][0]['instructions'][0]['parsed']=parsed
    with pytest.raises(ValueError):project(order,bank)


@pytest.mark.parametrize('kind',['assign','allocate'])
@pytest.mark.parametrize('placement',['top','binary','parsed'])
def test_old_or_bypassed_producer_flags_cannot_replace_consumer_revalidation(monkeypatch,kind,placement):
    order,bank=fixture_order(kind,placement)
    with monkeypatch.context() as context:
        context.setattr(authority_observations,'check',lambda *args:None)
        old_receipt=project(order,bank)
    assert old_receipt['observed_wallet_authority_mutations_rejected'] is True
    with pytest.raises(ValueError,match='wallet system-account mutation'):
        unsigned_projection.validate(order,old_receipt,rpc_source_sha256='a'*64,max_wallet_fee_lamports=6000)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['assign','allocate'])
@pytest.mark.parametrize('placement',['top','binary','parsed'])
@pytest.mark.parametrize('route',['metis','jupiterz','dflow','okx'])
async def test_actual_managed_adapter_rejects_mutation_before_sign_or_post(managed,http,monkeypatch,kind,placement,route):
    from unittest.mock import Mock
    from fetcher import jupiter_router as router
    from runtime import owned_dispatch
    from test_jupiter_managed_contract import Response
    import time
    order,bank=fixture_order(kind,placement,wallet=managed.PUBLIC_KEY)
    raw=order_payload(str(managed.PUBLIC_KEY),router_name=route)
    raw['transaction']=order.raw['transaction']
    http.replies.append(Response(raw))
    observed=[]
    async def observe(checked_order,*,endpoint,max_wallet_fee_lamports=None):
        observed.append(checked_order)
        return unsigned_projection.check(checked_order,bank,
            rpc_source_sha256=router.solana_execution.endpoint_fingerprint(endpoint),
            observed_at=datetime.now(timezone.utc).isoformat(),current_node_slot=449,
            max_wallet_fee_lamports=max_wallet_fee_lamports),time.monotonic()
    monkeypatch.setattr(router.solana_execution,'project_unsigned',observe)
    sign=Mock(side_effect=AssertionError('Synthetic rejected packet must never reach signing'))
    monkeypatch.setattr(managed,'sign_base64_transaction',sign)
    with pytest.raises(router.SwapPreparationError) as failure:
        await router.execute_managed_swap(input_mint=order.request.input_mint,output_mint=TOKEN,
            amount_lamports=AMOUNT,slippage_bps=100,max_wallet_fee_lamports=6000)
    assert len(observed)==1 and isinstance(failure.value.__cause__,ValueError)
    assert 'wallet system-account mutation' in str(failure.value.__cause__)
    sign.assert_not_called()
    assert [call[0] for call in http.calls]==['GET'] and owned_dispatch.pending_dispatch_count()==0


# Explicit imported fixture dependencies: detached synthetic signer, fake HTTP,
# configured-node project/reconciliation seams; no operator signing module.
from test_live_signing_boundaries import signer
from test_jupiter_managed_contract import managed,http
