"""Offline signed dummy packets; no wallet keys, providers or broadcast."""
import base64
from copy import deepcopy
import time

import pytest
from solders.hash import Hash
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.instruction import AccountMeta, Instruction
from solders.instruction import CompiledInstruction
from solders.keypair import Keypair
from solders.message import Message, MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from analytics.rpc_mint_initialization import inspect_initialization

LEGACY = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
EXTENSIONS = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
RENT = Pubkey.from_string('SysvarRent111111111111111111111111111111111')


def b58(value):
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    number, out = int.from_bytes(value, 'big'), ''
    while number:
        number, index = divmod(number, 58)
        out = alphabet[index] + out
    return '1' * (len(value)-len(value.lstrip(b'\0'))) + out


def fixture(*, program=LEGACY, opcode=20, v0=True, cpi=False, writable=True, lookup=False):
    # Hash.default is not an executable recent blockhash. These deterministic
    # ephemeral fixtures cannot be submitted as real financial transactions.
    payer = Keypair.from_seed(bytes([1])*32)
    mint, authority = Keypair.from_seed(bytes([2])*32).pubkey(), Keypair.from_seed(bytes([3])*32).pubkey()
    program_key = Pubkey.from_string(program)
    data = bytes([opcode, 9]) + bytes(authority) + b'\0'
    accounts = [AccountMeta(mint, False, writable)]
    if opcode == 0: accounts.append(AccountMeta(RENT, False, False))
    if cpi:
        outer = Instruction(Keypair.from_seed(bytes([4])*32).pubkey(), b'dummy-create',
                            accounts + [AccountMeta(program_key, False, False)])
    else: outer = Instruction(program_key, data, accounts)
    table = AddressLookupTableAccount(Keypair.from_seed(bytes([5])*32).pubkey(), [mint, RENT])
    message = (MessageV0.try_compile(payer.pubkey(), [outer], [table] if lookup else [], Hash.default()) if v0
               else Message.new_with_blockhash([outer], payer.pubkey(), Hash.default()))
    packet = VersionedTransaction(message, [payer])
    keys = list(message.account_keys)
    loaded = {'writable':[], 'readonly':[]}
    if v0:
        for item in message.address_table_lookups:
            for group in loaded:
                loaded[group] += [str(table.addresses[index]) for index in getattr(item, group+'_indexes')]
        keys += [Pubkey.from_string(key) for group in loaded for key in loaded[group]]
    inner = ([{'index':0, 'instructions':[{'programIdIndex':keys.index(program_key),
               'accounts':[keys.index(account.pubkey) for account in accounts], 'data':b58(data), 'stackHeight':2}]}]
             if cpi else [])
    received = time.time()
    result = {'slot':123, 'blockTime':int(received)-120, 'version':0 if v0 else 'legacy',
              'transaction':[base64.b64encode(bytes(packet)).decode(), 'base64'],
              'meta':{'err':None, 'loadedAddresses':loaded, 'innerInstructions':inner}}
    return result, str(mint), str(packet.signatures[0]), received


@pytest.mark.parametrize('program', [LEGACY, EXTENSIONS])
@pytest.mark.parametrize('opcode', [0, 20])
@pytest.mark.parametrize('v0', [False, True])
@pytest.mark.parametrize('cpi', [False, True])
def test_exact_mint_init_in_original_legacy_v0_and_cpi_packet(program, opcode, v0, cpi):
    result, mint, signature, received = fixture(program=program, opcode=opcode, v0=v0, cpi=cpi)
    checked = inspect_initialization(result, mint, signature, reference_slot=123, reference_received_at=received)
    assert checked['slot'] == 123 and checked['tx_hash'] == signature
    assert checked['evidence']['transaction'] == result['transaction']
    assert len(checked['rpc_evidence_sha256']) == 64


def test_signed_but_structurally_invalid_packet_cannot_certify_an_initializer():
    result, mint, signature, received = fixture()
    packet = VersionedTransaction.from_bytes(base64.b64decode(result['transaction'][0]))
    old = packet.message
    # A valid signature is not sufficient: invoking the fee payer as program
    # index zero is structurally invalid even alongside a valid initializer.
    message = MessageV0(old.header,old.account_keys,old.recent_blockhash,
        list(old.instructions)+[CompiledInstruction(0,b'',b'')],old.address_table_lookups)
    packet = VersionedTransaction(message,[Keypair.from_seed(bytes([1])*32)])
    result['transaction'][0] = base64.b64encode(bytes(packet)).decode()
    with pytest.raises(ValueError):
        inspect_initialization(result,mint,str(packet.signatures[0]),reference_received_at=received)


@pytest.mark.parametrize('change', ['wrong_mint','wrong_signature','unsigned','wrong_version','failed','missing_error',
                                  'missing_time','future_time','wrong_slot','bool_slot','bad_encoding',
                                  'bad_loaded_counts','unknown_program','supply_mint','token_account','only_pool'])
def test_unproved_initialization_cannot_supply_birth(change):
    opcode = {'supply_mint':7,'token_account':18,'only_pool':3}.get(change,20)
    program = str(Keypair.from_seed(bytes([9])*32).pubkey()) if change == 'unknown_program' else LEGACY
    result, mint, signature, received = fixture(opcode=opcode, program=program)
    if change == 'wrong_mint': mint = str(Keypair.from_seed(bytes([9])*32).pubkey())
    elif change == 'wrong_signature': signature = '1'*64
    elif change == 'unsigned':
        raw = bytearray(base64.b64decode(result['transaction'][0])); raw[1:65] = bytes(64)
        result['transaction'][0] = base64.b64encode(raw).decode()
    elif change == 'wrong_version': result['version'] = 'legacy'
    elif change == 'failed': result['meta']['err'] = {'InstructionError':[0,'InvalidArgument']}
    elif change == 'missing_error': result['meta'].pop('err')
    elif change == 'missing_time': result['blockTime'] = None
    elif change == 'future_time': result['blockTime'] = int(received)+1
    elif change == 'wrong_slot': result['slot'] = 124
    elif change == 'bool_slot': result['slot'] = True
    elif change == 'bad_encoding': result['transaction'][1] = 'base58'
    elif change == 'bad_loaded_counts': result['meta']['loadedAddresses']['writable'] = [mint]
    with pytest.raises(ValueError):
        inspect_initialization(result, mint, signature, reference_slot=123, reference_received_at=received)


@pytest.mark.parametrize('change', ['duplicate_parent','invalid_parent','bool_program','bad_account','ambiguous_init','invalid_base58'])
def test_cpi_recording_must_be_bounded_and_unambiguous(change):
    result, mint, signature, received = fixture(cpi=True)
    groups = result['meta']['innerInstructions']
    if change == 'duplicate_parent': groups.append(deepcopy(groups[0]))
    elif change == 'invalid_parent': groups[0]['index'] = 1
    elif change == 'bool_program': groups[0]['instructions'][0]['programIdIndex'] = True
    elif change == 'bad_account': groups[0]['instructions'][0]['accounts'] = [999]
    elif change == 'ambiguous_init': groups[0]['instructions'].append(deepcopy(groups[0]['instructions'][0]))
    elif change == 'invalid_base58': groups[0]['instructions'][0]['data'] = '0'*64
    with pytest.raises(ValueError):
        inspect_initialization(result, mint, signature, reference_received_at=received)


@pytest.mark.parametrize('v0,cpi', [(False,False),(False,True),(True,False),(True,True)])
def test_readonly_mint_cannot_be_initialized_even_if_rpc_claims_success(v0, cpi):
    result, mint, signature, received = fixture(v0=v0, cpi=cpi, writable=False)
    with pytest.raises(ValueError):
        inspect_initialization(result, mint, signature, reference_received_at=received)
