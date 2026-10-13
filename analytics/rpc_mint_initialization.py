"""Verified signed message + trusted-node mint initialization, not finality."""
from copy import deepcopy
import base64
import datetime as dt
import hashlib
import json

from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

VERSION = 'rpc_mint_initialization_receipt_v1'
BASIS = 'rpc_reported_mint_initialization_not_independent_chain_history_verification'
TOKEN_PROGRAMS = {'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                  'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'}
_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
MAX_EVIDENCE_BYTES = 65536


def _uint(value, maximum=2**64-1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('Invalid initialization integer')
    return value


def _address(value):
    if not isinstance(value, str) or str(Pubkey.from_string(value)) != value:
        raise ValueError('Invalid initialization account identity')
    return value


def _decode58(value):
    if not isinstance(value, str) or len(value) > 16384:
        raise ValueError('Invalid initialization instruction encoding')
    number = 0
    for char in value:
        if char not in _ALPHABET:
            raise ValueError('Invalid initialization instruction encoding')
        number = number * 58 + _ALPHABET.index(char)
    return b'\0' * (len(value) - len(value.lstrip('1'))) + number.to_bytes((number.bit_length()+7)//8, 'big')


def compact_evidence(result):
    if not isinstance(result, dict) or not isinstance(result.get('meta'), dict):
        raise ValueError('Missing initialization transaction evidence')
    meta = result['meta']
    out = {key: result.get(key) for key in ('slot', 'blockTime', 'version', 'transaction')}
    out['meta'] = {key: meta[key] for key in ('err', 'status', 'loadedAddresses', 'innerInstructions') if key in meta}
    try:
        if len(json.dumps(out, separators=(',', ':'), allow_nan=False).encode()) > MAX_EVIDENCE_BYTES:
            raise ValueError('Initialization evidence exceeds bound')
        return deepcopy(out)
    except (TypeError, RecursionError):
        raise ValueError('Initialization evidence is malformed') from None


def inspect_initialization(result, address, signature, *, reference_slot=None, reference_received_at):
    """Reconstruct exact mint initialization; RPC metadata remains node-reported.

    No signer, wallet key, HTTP or state mutation. Legacy and v0 packets are
    supported. Token initialization in top-level and recorded CPI instructions
    is distinguished from minting supply, pool creation and token accounts.
    This is not an archive search proving the first lifetime of an address.
    """
    evidence = compact_evidence(result)
    address = _address(address)
    slot, block_time = _uint(evidence['slot']), _uint(evidence['blockTime'], 2**63-1)
    if not slot or not block_time or reference_slot is not None and _uint(reference_slot) != slot:
        raise ValueError('Initialization slot/time conflicts')
    if type(reference_received_at) not in (int, float) or not 0 < block_time <= reference_received_at < 1e11:
        raise ValueError('Initialization clock is not causal')
    encoded = evidence['transaction']
    if not isinstance(encoded, list) or len(encoded) != 2 or encoded[1] != 'base64' or not isinstance(encoded[0], str):
        raise ValueError('Initialization requires original binary transaction')
    raw = base64.b64decode(encoded[0], validate=True)
    if not 1 <= len(raw) <= 1232:
        raise ValueError('Initialization packet exceeds network bound')
    transaction = VersionedTransaction.from_bytes(raw)
    try:
        transaction.sanitize()
    except Exception:
        raise ValueError('Initialization packet is structurally invalid') from None
    if bytes(transaction) != raw or not transaction.signatures or not all(transaction.verify_with_results()):
        raise ValueError('Initialization message/signatures do not verify')
    message = transaction.message
    if len(transaction.signatures) != message.header.num_required_signatures or str(transaction.signatures[0]) != signature:
        raise ValueError('Initialization first signature is not the queried signature')
    version = evidence['version']
    if isinstance(message, MessageV0):
        if type(version) is not int or version != 0: raise ValueError('Initialization packet version conflicts')
    elif version != 'legacy':
        raise ValueError('Initialization packet version conflicts')
    meta = evidence['meta']
    if 'err' not in meta or meta['err'] is not None or 'status' in meta and meta['status'] != {'Ok': None}:
        raise ValueError('Initialization transaction did not succeed')
    lookups = message.address_table_lookups if isinstance(message, MessageV0) else []
    loaded = meta.get('loadedAddresses', {'writable': [], 'readonly': []} if not lookups else None)
    if (not isinstance(loaded, dict) or set(loaded) != {'writable', 'readonly'}
            or not all(isinstance(loaded[key], list) for key in loaded)
            or len(loaded['writable']) != sum(len(item.writable_indexes) for item in lookups)
            or len(loaded['readonly']) != sum(len(item.readonly_indexes) for item in lookups)):
        raise ValueError('Initialization loaded-address counts conflict')
    keys = [str(key) for key in message.account_keys] + [_address(key) for key in loaded['writable'] + loaded['readonly']]
    if len(keys) > 256 or len(set(keys)) != len(keys):
        raise ValueError('Initialization account identity is ambiguous')
    outer = list(message.instructions)
    if len(outer) > 128: raise ValueError('Initialization instruction count exceeds bound')
    instructions = [(int(item.program_id_index), list(item.accounts), bytes(item.data)) for item in outer]
    inner = meta.get('innerInstructions')
    if inner is not None:
        if not isinstance(inner, list) or len(inner) > 128: raise ValueError('Invalid initialization CPI recording')
        seen = set()
        for group in inner:
            if not isinstance(group, dict) or type(group.get('index')) is not int or not 0 <= group['index'] < len(outer) or group['index'] in seen:
                raise ValueError('Ambiguous initialization CPI parent')
            seen.add(group['index'])
            rows = group.get('instructions')
            if not isinstance(rows, list) or len(rows) > 128: raise ValueError('Invalid initialization CPI instructions')
            for item in rows:
                if not isinstance(item, dict) or type(item.get('programIdIndex')) is not int or not isinstance(item.get('accounts'), list):
                    raise ValueError('Invalid initialization CPI format')
                instructions.append((item['programIdIndex'], item['accounts'], _decode58(item.get('data'))))
    if len(instructions) > 256: raise ValueError('Initialization instruction count exceeds bound')
    matches = []
    for program_index, accounts, data in instructions:
        if not 0 <= program_index < len(keys) or any(type(index) is not int or not 0 <= index < len(keys) for index in accounts):
            raise ValueError('Initialization instruction account index conflicts')
        if keys[program_index] not in TOKEN_PROGRAMS or not data or data[0] not in (0, 20): continue
        # Both standard token programs use the same InitMint/InitMint2 prefix.
        # The documented unpacker ignores trailing bytes after COption; no
        # library-specific fixed padding is mistaken for a different action.
        if len(data) < 35 or data[34] not in (0, 1) or data[34] == 1 and len(data) < 67:
            raise ValueError('Malformed mint initialization instruction')
        required = 2 if data[0] == 0 else 1
        if len(accounts) < required: raise ValueError('Missing mint initialization accounts')
        if keys[accounts[0]] == address:
            mint_index = accounts[0]
            static_count = len(message.account_keys)
            signed_count = message.header.num_required_signatures
            writable = (mint_index < signed_count - message.header.num_readonly_signed_accounts
                        if mint_index < signed_count else
                        mint_index < static_count - message.header.num_readonly_unsigned_accounts
                        if mint_index < static_count else
                        mint_index < static_count + len(loaded['writable']))
            if not writable:
                raise ValueError('Mint initialization account is not writable')
            if data[0] == 0 and keys[accounts[1]] != 'SysvarRent111111111111111111111111111111111':
                raise ValueError('Mint initialization rent identity conflicts')
            matches.append((keys[program_index], data[0]))
    if len(matches) != 1:
        raise ValueError('Exact unambiguous mint initialization not observed')
    return {'created_at': dt.datetime.fromtimestamp(block_time, dt.timezone.utc).isoformat(),
            'slot': slot, 'tx_hash': signature, 'evidence': evidence,
            'rpc_evidence_sha256': hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()}
