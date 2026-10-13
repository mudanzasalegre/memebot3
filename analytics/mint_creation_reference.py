"""Bounded untrusted discovery reference; never a birth clock or buy permission."""
from copy import deepcopy
import datetime as dt
import math
import time

from analytics.token_birth import base58_size, birth_clock

FIELD = 'mint_creation_reference'
VERSION = 'pumpportal_creation_reference_v1'
BASIS = 'untrusted_creation_reference_not_mint_birth'
_KEYS = {'version', 'source', 'chain', 'address', 'tx_hash', 'slot', 'received_at', 'basis'}


def checked_reference(raw, address, *, now=None):
    limit = time.time() if now is None else now.timestamp() if isinstance(now, dt.datetime) else now
    if (not isinstance(raw, dict) or set(raw) != _KEYS or raw.get('version') != VERSION
            or raw.get('source') != 'pumpportal' or raw.get('chain') != 'solana'
            or raw.get('basis') != BASIS or raw.get('address') != address
            or not base58_size(address, 32) or not base58_size(raw.get('tx_hash'), 64)
            or raw.get('slot') is not None and (type(raw['slot']) is not int or not 0 < raw['slot'] < 2**64)
            or type(raw.get('received_at')) not in (int, float) or type(limit) not in (int, float)):
        return None
    try:
        valid = math.isfinite(raw['received_at']) and math.isfinite(limit) and 0 < raw['received_at'] <= limit < 1e11
    except (ValueError, TypeError, OverflowError):
        valid = False
    return deepcopy(raw) if valid else None


def _one_alias(sources, aliases):
    values = [part[key] for part in sources for key in aliases if key in part and part[key] is not None]
    if not values: return None
    # Conflicting aliases must not choose a convenient transaction or slot.
    if any(type(value) is not type(values[0]) or value != values[0] for value in values):
        raise ValueError('Ambiguous creation reference')
    return values[0]


def reference_from_event(message, address, received_at):
    if not isinstance(message, dict): return None
    sources = [message]
    if isinstance(message.get('data'), dict): sources.append(message['data'])
    try:
        kind = _one_alias(sources, ('txType', 'tx_type'))
        if kind is not None and kind != 'create': return None
        signature = _one_alias(sources, ('signature', 'txHash', 'tx_hash', 'transactionSignature'))
        slot = _one_alias(sources, ('slot',))
    except ValueError:
        return None
    raw = {'version': VERSION, 'source': 'pumpportal', 'chain': 'solana', 'address': address,
           'tx_hash': signature, 'slot': slot, 'received_at': received_at, 'basis': BASIS}
    return checked_reference(raw, address, now=received_at)


def merge_reference(primary, secondary, *, now=None):
    out = deepcopy(primary)
    if not isinstance(secondary, dict) or secondary.get('address') != out.get('address'):
        return out
    current = checked_reference(out.get(FIELD), out.get('address'), now=now)
    incoming = checked_reference(secondary.get(FIELD), out.get('address'), now=now)
    if current is not None and (incoming is None or current['tx_hash'] == incoming['tx_hash'] or birth_clock(out) is not None):
        out[FIELD] = current  # Never rejuvenate a repeated reference's receipt.
    elif incoming is not None:
        out[FIELD] = incoming
    elif FIELD in out:
        out.pop(FIELD)
    return out
