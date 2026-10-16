"""Detached synthetic signer/SDK packets; no operator key, RPC or broadcast."""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from solders.hash import Hash
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.instruction import AccountMeta,Instruction,CompiledInstruction
from solders.keypair import Keypair
from solders.message import Message,MessageV0,MessageHeader,to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction,VersionedTransaction
from solders import system_program

from chain_fixtures import INPUT_ACCOUNT,POOL
from test_live_signing_boundaries import signer
from test_jupiter_dispatch_boundaries import http, Response
from test_jupiter_quote_contract import payload, checked

LEGACY=Pubkey.from_string('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
TOKEN22=Pubkey.from_string('TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb')


def mutation(wallet,kind):
    if kind=='assign':return system_program.assign({'pubkey':wallet,'owner':LEGACY})
    if kind=='allocate':return system_program.allocate({'pubkey':wallet,'space':16})
    family,opcode=kind.split(':');opcode=int(opcode)
    program=LEGACY if family=='legacy' else TOKEN22
    accounts=[AccountMeta(INPUT_ACCOUNT,False,True),AccountMeta(POOL,False,False),AccountMeta(wallet,True,False)]
    data=bytes([opcode])+(1000).to_bytes(8,'little')
    if opcode==6:
        accounts=[AccountMeta(INPUT_ACCOUNT,False,True),AccountMeta(wallet,True,False)]
        data=bytes([6,3,1])+bytes(POOL)
    if opcode==13:
        accounts.insert(1,AccountMeta(LEGACY,False,False));data+=bytes([6])
    return Instruction(program,data,accounts)


def packet(wallet,kind,version):
    ix=mutation(wallet,kind)
    if version=='legacy':return bytes(Transaction.new_unsigned(Message.new_with_blockhash([ix],wallet,Hash.new_unique())))
    message=MessageV0.try_compile(wallet,[ix],[],Hash.new_unique())
    return bytes(VersionedTransaction.populate(message,[Signature.default()]*message.header.num_required_signatures))


def watch_key(signer,monkeypatch):
    key=signer.KEYPAIR;calls=[]
    def sign(message):calls.append(bytes(message));return key.sign_message(message)
    monkeypatch.setattr(signer,'KEYPAIR',SimpleNamespace(pubkey=key.pubkey,sign_message=sign))
    return calls


KINDS=('assign','allocate','legacy:4','legacy:6','legacy:13','token22:4','token22:6','token22:13')


def no_io(signer,monkeypatch):
    spies=[]
    for name in ('_send_via_rpc','_send_via_jito','_client_for_url'):
        spy=Mock(side_effect=AssertionError('Unexpected synthetic RPC/relay'))
        monkeypatch.setattr(signer,name,spy);spies.append(spy)
    return spies


def assert_no_io(spies):
    for spy in spies:spy.assert_not_called()


def ordinary(wallet,version,*,lookup=False):
    ix=system_program.transfer({'from_pubkey':wallet,'to_pubkey':POOL,'lamports':100_000_000})
    if version=='legacy':
        return bytes(Transaction.new_unsigned(Message.new_with_blockhash([ix],wallet,Hash.new_unique())))
    tables=[AddressLookupTableAccount(Pubkey.new_unique(),[POOL])] if lookup else []
    message=MessageV0.try_compile(wallet,[ix],tables,Hash.new_unique())
    return bytes(VersionedTransaction.populate(message,[Signature.default()]))


def signed_by_fixture(raw,key):
    tx=VersionedTransaction.from_bytes(raw)
    return bytes(VersionedTransaction.populate(tx.message,[key.sign_message(to_bytes_versioned(tx.message))]))


@pytest.mark.parametrize('kind',KINDS)
@pytest.mark.parametrize('version',['legacy','v0'])
def test_generic_signer_rejects_known_wallet_mutations_before_private_signing(signer,monkeypatch,kind,version):
    raw=packet(signer.PUBLIC_KEY,kind,version);calls=watch_key(signer,monkeypatch)
    with pytest.raises(ValueError,match='wallet|delegation|authority'):signer.sign_raw_transaction(raw)
    assert calls==[]


@pytest.mark.parametrize('version',['legacy','v0'])
def test_noncanonical_trailing_packet_is_not_silently_reserialized_and_signed(signer,monkeypatch,version):
    ix=system_program.transfer({'from_pubkey':signer.PUBLIC_KEY,'to_pubkey':POOL,'lamports':100_000_000})
    if version=='legacy':raw=bytes(Transaction.new_unsigned(Message.new_with_blockhash([ix],signer.PUBLIC_KEY,Hash.new_unique())))
    else:
        message=MessageV0.try_compile(signer.PUBLIC_KEY,[ix],[],Hash.new_unique())
        raw=bytes(VersionedTransaction.populate(message,[Signature.default()]))
    calls=watch_key(signer,monkeypatch)
    with pytest.raises(Exception):signer.sign_raw_transaction(raw+b'\x00')
    assert calls==[]


@pytest.mark.parametrize('kind',KINDS)
@pytest.mark.parametrize('version',['legacy','v0'])
@pytest.mark.parametrize('entry',['sign_base64','dispatch_bytes','dispatch_base64','send_bytes','send_base64'])
def test_every_raw_entry_rejects_known_mutation_before_signature_or_transport(signer,monkeypatch,kind,version,entry):
    raw=packet(signer.PUBLIC_KEY,kind,version)
    if entry.startswith('send_'):raw=signed_by_fixture(raw,signer.KEYPAIR)
    encoded=base64.b64encode(raw).decode('ascii')
    calls=watch_key(signer,monkeypatch);spies=no_io(signer,monkeypatch)
    with pytest.raises(ValueError,match='wallet|delegation|authority'):
        if entry=='sign_base64':signer.sign_base64_transaction(encoded)
        elif entry.startswith('dispatch_'):signer.sign_and_send(raw if entry=='dispatch_bytes' else encoded)
        else:signer.send_raw_transaction(raw if entry=='send_bytes' else encoded,prefer_jito=True)
    assert calls==[];assert_no_io(spies)


@pytest.mark.parametrize('kind',KINDS)
def test_transaction_object_rejected_before_blockhash_read(signer,monkeypatch,kind):
    raw=packet(signer.PUBLIC_KEY,kind,'legacy')
    tx=Transaction.from_bytes(raw);calls=watch_key(signer,monkeypatch);spies=no_io(signer,monkeypatch)
    with pytest.raises(ValueError,match='wallet|delegation|authority'):signer.sign_and_send(tx)
    assert calls==[] and bytes(tx)==raw;assert_no_io(spies)


@pytest.mark.parametrize('version,lookup',[('legacy',False),('v0',False),('v0',True)])
def test_canonical_packet_preserves_message_and_exact_point_one_sol_control(signer,monkeypatch,version,lookup):
    raw=ordinary(signer.PUBLIC_KEY,version,lookup=lookup)
    original=VersionedTransaction.from_bytes(raw);calls=watch_key(signer,monkeypatch)
    signed=VersionedTransaction.from_bytes(signer.sign_raw_transaction(raw))
    assert signed.message==original.message and signed.verify_with_results()==[True]
    assert calls==[to_bytes_versioned(original.message)]
    assert bytes(signed.message.instructions[0].data)==bytes([2,0,0,0])+(100_000_000).to_bytes(8,'little')
    if lookup:assert len(signed.message.address_table_lookups)==1
    # A static signature control is not approval of arbitrary financial intent.


@pytest.mark.parametrize('version',['legacy','v0'])
@pytest.mark.parametrize('sponsored',[False,True])
@pytest.mark.parametrize('cosignature',['valid','placeholder','invalid'])
def test_original_foreign_cosignatures_preserved_or_rejected_before_wallet_signing(signer,monkeypatch,version,sponsored,cosignature):
    other=Keypair.from_seed(bytes([7])*32);payer=other.pubkey() if sponsored else signer.PUBLIC_KEY
    ix=Instruction(Pubkey.new_unique(),b'fixture',[AccountMeta(signer.PUBLIC_KEY,True,False),AccountMeta(other.pubkey(),True,False)])
    msg=(Message.new_with_blockhash([ix],payer,Hash.new_unique()) if version=='legacy'
        else MessageV0.try_compile(payer,[ix],[],Hash.new_unique()))
    keys=list(msg.account_keys[:msg.header.num_required_signatures]);index=keys.index(other.pubkey())
    signatures=[Signature.default()]*len(keys)
    signatures[index]=(other.sign_message(to_bytes_versioned(msg)) if cosignature=='valid'
        else Signature.default() if cosignature=='placeholder' else Signature.from_bytes(bytes([9])*64))
    raw=bytes(VersionedTransaction.populate(msg,signatures));calls=watch_key(signer,monkeypatch);spies=no_io(signer,monkeypatch)
    if cosignature=='invalid':
        with pytest.raises(ValueError,match='cosignature'):signer.sign_raw_transaction(raw)
        assert calls==[]
    else:
        actual=VersionedTransaction.from_bytes(signer.sign_raw_transaction(raw))
        assert actual.message==msg and actual.signatures[index]==signatures[index]
        assert actual.verify_with_results()[keys.index(signer.PUBLIC_KEY)] is True
        if cosignature=='placeholder':
            with pytest.raises(ValueError,match='all original'):signer.send_raw_transaction(bytes(actual))
        assert calls==[to_bytes_versioned(msg)]
    assert_no_io(spies)


@pytest.mark.parametrize('version',['legacy','v0'])
@pytest.mark.parametrize('kind',KINDS)
def test_other_original_signer_not_mistaken_for_wallet_mutation(signer,monkeypatch,version,kind):
    other=Keypair.from_seed(bytes([7])*32);ix=mutation(other.pubkey(),kind)
    msg=(Message.new_with_blockhash([ix],signer.PUBLIC_KEY,Hash.new_unique()) if version=='legacy'
        else MessageV0.try_compile(signer.PUBLIC_KEY,[ix],[],Hash.new_unique()))
    keys=list(msg.account_keys[:msg.header.num_required_signatures]);signatures=[Signature.default()]*len(keys)
    signatures[keys.index(other.pubkey())]=other.sign_message(to_bytes_versioned(msg))
    actual=VersionedTransaction.from_bytes(signer.sign_raw_transaction(bytes(VersionedTransaction.populate(msg,signatures))))
    assert actual.message==msg and all(actual.verify_with_results())
    assert actual.signatures[keys.index(other.pubkey())]==signatures[keys.index(other.pubkey())]


@pytest.mark.parametrize('shape',['empty','junk','oversize','text','bytearray','none','trailing','overlong-shortvec'])
@pytest.mark.parametrize('entry',['sign','send'])
def test_invalid_original_packet_has_no_wallet_signature_or_transport(signer,monkeypatch,shape,entry):
    raw=ordinary(signer.PUBLIC_KEY,'v0')
    values={'empty':b'','junk':b'not a transaction','oversize':b'x'*1233,'text':'not base64',
        'bytearray':bytearray(raw),'none':None,'trailing':raw+b'\0',
        'overlong-shortvec':b'\x81\x00'+raw[1:]}
    calls=watch_key(signer,monkeypatch);spies=no_io(signer,monkeypatch)
    with pytest.raises(Exception):
        if entry=='sign':signer.sign_raw_transaction(values[shape])
        else:signer.send_raw_transaction(values[shape],prefer_jito=True)
    assert calls==[];assert_no_io(spies)


@pytest.mark.parametrize('version',['legacy','v0'])
def test_packet_bound_includes_exact_1232_canonical_bytes(signer,version):
    def build(size):
        ix=Instruction(Pubkey.new_unique(),b'x'*size,[])
        msg=(Message.new_with_blockhash([ix],signer.PUBLIC_KEY,Hash.new_unique()) if version=='legacy'
            else MessageV0.try_compile(signer.PUBLIC_KEY,[ix],[],Hash.new_unique()))
        return bytes(VersionedTransaction.populate(msg,[Signature.default()]))
    size=900+1232-len(build(900));raw=build(size)
    assert len(raw)==1232
    assert VersionedTransaction.from_bytes(signer.sign_raw_transaction(raw)).verify_with_results()==[True]
    with pytest.raises(ValueError,match='size'):signer.sign_raw_transaction(build(size+1))


@pytest.mark.parametrize('version',['legacy','v0'])
@pytest.mark.parametrize('shape',['missing-signature','extra-signature','account-index','program-index'])
@pytest.mark.parametrize('entry',['sign','send'])
def test_sdk_parsed_but_unsanitized_packet_rejected_before_key_or_io(signer,monkeypatch,version,shape,entry):
    ix=CompiledInstruction(99 if shape=='program-index' else 1,b'fixture',bytes([99 if shape=='account-index' else 0]))
    keys=[signer.PUBLIC_KEY,Pubkey.new_unique()]
    msg=(Message.new_with_compiled_instructions(1,0,1,keys,Hash.new_unique(),[ix]) if version=='legacy'
        else MessageV0(MessageHeader(1,0,1),keys,Hash.new_unique(),[ix],[]))
    count=0 if shape=='missing-signature' else 2 if shape=='extra-signature' else 1
    raw=bytes(VersionedTransaction.populate(msg,[Signature.default()]*count))
    assert VersionedTransaction.from_bytes(raw).message==msg
    calls=watch_key(signer,monkeypatch);spies=no_io(signer,monkeypatch)
    with pytest.raises(ValueError,match='Invalid original transaction'):
        if entry=='sign':signer.sign_raw_transaction(raw)
        else:signer.send_raw_transaction(raw,prefer_jito=True)
    assert calls==[];assert_no_io(spies)


@pytest.mark.asyncio
@pytest.mark.parametrize('side',['buy','sell'])
@pytest.mark.parametrize('version',['legacy','v0'])
@pytest.mark.parametrize('kind',['assign','legacy:4','token22:6'])
async def test_native_gmgn_hits_shared_guard_once_without_signature_or_send(signer,monkeypatch,side,version,kind):
    import trader
    from trader import gmgn
    from runtime import owned_dispatch
    raw=packet(signer.PUBLIC_KEY,kind,version)
    route=AsyncMock(return_value={'data':{'raw_tx':{'swapTransaction':base64.b64encode(raw).decode('ascii')}}})
    monkeypatch.setitem(trader.__dict__,'sol_signer',signer);monkeypatch.setattr(gmgn,'_route',route)
    calls=watch_key(signer,monkeypatch);spies=no_io(signer,monkeypatch)
    with pytest.raises(ValueError,match='wallet|delegation|authority'):
        if side=='buy':await gmgn.buy(str(LEGACY),.1)
        else:await gmgn.sell(str(LEGACY),1000)
    route.assert_awaited_once();assert calls==[];assert_no_io(spies)
    assert owned_dispatch.pending_dispatch_count()==0


@pytest.mark.asyncio
@pytest.mark.parametrize('version',['legacy','v0'])
@pytest.mark.parametrize('kind',['assign','legacy:4','token22:6'])
async def test_native_jupiter_guard_does_not_create_retry_or_uncertainty_bypass(signer,monkeypatch,http,version,kind):
    import trader
    from fetcher import jupiter_router as router
    from runtime import owned_dispatch
    raw=packet(signer.PUBLIC_KEY,kind,version)
    monkeypatch.setitem(trader.__dict__,'sol_signer',signer);monkeypatch.setenv('SOL_PUBLIC_KEY',str(signer.PUBLIC_KEY))
    http.responses.append(Response({'swapTransaction':base64.b64encode(raw).decode('ascii'),'lastValidBlockHeight':123}))
    calls=watch_key(signer,monkeypatch);spies=no_io(signer,monkeypatch)
    with pytest.raises(router.SwapSubmissionUncertain) as caught:
        await router.execute_swap(checked(payload()),max_retries=5)
    assert isinstance(caught.value.__cause__,ValueError)
    assert len(http.calls)==len(http.closed)==1 and http.delays==[]
    assert calls==[];assert_no_io(spies)
    assert owned_dispatch.pending_dispatch_count()==0
    # Outer dispatch remains conservative; an exception type alone is no no-send proof.
