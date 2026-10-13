"""One bounded read-only mint initializer query; no retry, signing or broadcast."""
from copy import deepcopy
import datetime as dt
import math
import time

import aiohttp

from analytics.mint_creation_reference import checked_reference
from analytics.rpc_mint_initialization import VERSION, BASIS, inspect_initialization
from analytics.token_birth import FIELD, checked_token_birth
from utils import solana_execution as rpc


async def get_mint_initialization(reference, *, endpoint=None, timeout_s=3., wall=time.time):
    if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not .05 <= timeout_s <= 8.:
        raise ValueError('Mint initialization RPC timeout is invalid')
    address = reference.get('address') if isinstance(reference, dict) else None
    reference = checked_reference(reference, address, now=wall())
    if reference is None:
        return None
    try:
        url = rpc.configured_endpoint() if endpoint is None else rpc.checked_endpoint(endpoint)
        source_hash = rpc.endpoint_fingerprint(url)
    except (ValueError, TypeError, IndexError):
        raise ValueError('Mint initialization RPC configuration is invalid') from None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session:
        result = await rpc._read(session, url, 'getTransaction', [reference['tx_hash'],
            {'encoding':'base64', 'commitment':'confirmed', 'maxSupportedTransactionVersion':0}])
    received = wall()
    if result is None:
        return None
    checked = inspect_initialization(result, address, reference['tx_hash'],
        reference_slot=reference['slot'], reference_received_at=reference['received_at'])
    proof = {'version':VERSION, 'source':'solana_rpc', 'chain':'solana', 'address':address,
        'created_at':checked['created_at'], 'slot':checked['slot'], 'tx_hash':checked['tx_hash'],
        'received_at':received, 'basis':BASIS, 'reference':deepcopy(reference),
        'evidence':checked['evidence'], 'rpc_source_sha256':source_hash,
        'rpc_evidence_sha256':checked['rpc_evidence_sha256']}
    proof = checked_token_birth(proof, address, now=received)
    if proof is None:
        return None
    return {'address':address, 'created_at':dt.datetime.fromisoformat(proof['created_at']), FIELD:proof}
